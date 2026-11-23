"""End to end with a real dispatcher and Celery worker configured for the OpenAI-compatible
provider, talking to a fake HTTP server: jobs succeed with usage recorded everywhere, a planted
secret never leaves the machine, a wrong key fails at once, and the key itself is found nowhere:
not in any database row, stored file, log line, span or API response."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import Base
from devtools import zip_samples
from tests import telemetry
from tests.fake_provider import FakeProviderServer, Reply
from tests.integration import test_e2e_pipeline as pipeline_tests

pytestmark = pytest.mark.e2e

api_settings = pipeline_tests.api_settings
start_pipeline = pipeline_tests.start_pipeline
submit_job, wait_until, job_status = (
    pipeline_tests.submit_job,
    pipeline_tests.wait_until,
    pipeline_tests.job_status,
)

KEY = "e2e-fake-provider-key-7f3a9c1e5b2d4f60"
PLANTED = "planted-e2e-db-password-31337"
CONFIG_PY = f'DATABASE_URL = "postgresql://shop:{PLANTED}@db/shop"\nTIMEOUT = 30\n'


@pytest.fixture
def fake():
    with FakeProviderServer() as server:
        yield server


def real_pipeline(start_pipeline, fake):
    return start_pipeline(
        PROVIDER="openai_compatible",
        PROVIDER_BASE_URL=fake.base_url,
        PROVIDER_MODEL="fake-model-1",
        PROVIDER_API_KEY=KEY,
        PROVIDER_INPUT_PRICE_PER_MILLION_TOKENS="0.05",
        PROVIDER_OUTPUT_PRICE_PER_MILLION_TOKENS="0.08",
        PROVIDER_MAX_CALLS_PER_MINUTE="6000",
    )


def run(api, workflow: str, content: bytes | None = None, expect: str = "succeeded") -> dict:
    job_id = submit_job(api, workflow, content)
    wait_until(lambda: job_status(api, job_id) in ("succeeded", "failed"), 120, "job finished")
    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == expect, detail
    return detail


def database_text(engine) -> str:
    """Every row of every table, as JSON text."""
    with Session(engine) as session:
        return "\n".join(
            row
            for table in Base.metadata.sorted_tables
            for row in session.scalars(text(f"SELECT row_to_json(t)::text FROM {table.name} t"))
        )


def stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def test_real_provider_jobs_record_usage_and_the_key_is_found_nowhere(
    api,
    engine,
    start_pipeline,
    fake,
    queue_settings,
):
    pipeline = real_pipeline(start_pipeline, fake)

    pdf = run(api, "pdf_ingest")
    repo = run(
        api, "repo_index", zip_samples.small_repo(extra=[("src/config.py", CONFIG_PY.encode())])
    )
    fake.reply(Reply(status=401, body={"error": {"code": "invalid_api_key", "message": KEY}}))
    wrong_key = run(api, "pdf_ingest", expect="failed")

    # The provider was really used, with the key, in the Authorization header only.
    assert fake.requests and all(
        r.headers["Authorization"] == f"Bearer {KEY}" for r in fake.requests
    )
    assert all(KEY not in json.dumps(r.body) for r in fake.requests)

    # Usage per job, with the estimated cost (prices are configured in the worker; the in-process
    # API has none, so it shows tokens only).
    for detail in (pdf, repo):
        [usage] = detail["usage"]
        assert usage["provider"] == "openai_compatible" and usage["model"] == "fake-model-1"
        assert usage["total_tokens"] == 150 * usage["calls"] > 0
        manifest = next(a for a in detail["artifacts"] if a["artifact_type"] == "manifest")
        body = api.get(manifest["download_url"]).json()
        assert body["usage"]["estimated_cost"]["currency"] == "USD"
        assert body["usage"]["prompt_tokens"] == 120 * usage["calls"]

    # The planted secret never left the machine; the manifest has only its count.
    assert all(PLANTED not in json.dumps(r.body) for r in fake.requests)
    manifest = next(a for a in repo["artifacts"] if a["artifact_type"] == "manifest")
    scrubbing = api.get(manifest["download_url"]).json()["secret_scrubbing"]
    assert scrubbing["replaced"] >= 1 and scrubbing["by_kind"]["url_credentials"] >= 1

    # A wrong key: one attempt, permanent, no retry.
    assert wrong_key["error_class"] == "provider_auth"
    assert [a["outcome"] for a in wrong_key["attempts"]] == ["failed_permanent"]

    # Metrics and spans carry the usage.
    metrics = api.get("/metrics").text
    assert (
        'tasklane_provider_prompt_tokens_total{model="fake-model-1",provider="openai_compatible"}'
        in metrics
    )
    provider_spans = [s for s in pipeline.spans() if s["name"] == "provider.summarize"]
    assert any(s["attributes"].get("usage.total_tokens") == 150 for s in provider_spans)
    assert {s["attributes"]["provider"] for s in provider_spans} == {"openai_compatible"}

    # The key is nowhere: database, files, logs, spans, API responses.
    assert KEY not in database_text(engine)
    for path in stored_files(queue_settings.storage_root):
        assert KEY.encode() not in path.read_bytes(), path
    for log in (pipeline.worker_log, pipeline.dispatcher_log):
        assert KEY not in pipeline.read(log)
    assert KEY not in pipeline.span_file.read_text()
    assert KEY not in json.dumps(telemetry.captured())
    job_ids = [pdf["id"], repo["id"], wrong_key["id"]]
    responses = [api.get(url).text for url in ("/v1/meta", "/v1/overview", "/v1/jobs", "/metrics")]
    for job_id in job_ids:
        responses += [
            api.get(f"/v1/jobs/{job_id}").text,
            api.get(f"/v1/jobs/{job_id}/events/history").text,
        ]
    for detail in (pdf, repo):
        responses += [api.get(a["download_url"]).text for a in detail["artifacts"]]
    assert all(KEY not in body for body in responses)
    assert uuid.UUID(pdf["id"])  # sanity: real ids were scanned
