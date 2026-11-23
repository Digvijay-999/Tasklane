"""The real stages with the OpenAI-compatible provider and a fake HTTP server: secrets are
replaced before any text leaves the machine (repository and PDF), only counts reach the manifest,
usage and the estimated cost are recorded, a retry resumes from cached summaries, and another
model never reuses them."""

import json
from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.domain.errors import JobError
from app.observability.redaction import REDACTED, scrub
from app.providers.openai_compatible import OpenAICompatibleProvider
from devtools import pdf_samples, zip_samples
from tests.fake_provider import USAGE, FakeProviderServer, Reply
from tests.unit import test_repo_stages as repo_tests
from tests.unit.test_pdf_stages_rerun import RecordingContext

make_pipeline, run_all = repo_tests.make_pipeline, repo_tests.run_all
small_settings = repo_tests.small_settings

KEY = "fake-provider-key-for-stage-tests-42"
# Fake secrets planted in a repository file and a PDF. None may reach the provider.
PLANTED = {
    "url_credentials": "planted-db-pass-777",
    "assignment": "planted-token-value-8a8a8a8a",
    "token": "sk-plantedFAKEkey0123456789abcdef",
    "private_key": "MIIplantedprivatekeymaterial",
    "authorization": "planted-bearer-token-123456",
    "env_line": "planted-aws-secret-xyz",
}
CONFIG_PY = f'''"""Settings for the shop service."""

DATABASE_URL = "postgresql://admin:{PLANTED["url_credentials"]}@db.internal/shop"
API_TOKEN = "{PLANTED["assignment"]}"
OPENAI_KEY = "{PLANTED["token"]}"
PRIVATE = """-----BEGIN RSA PRIVATE KEY-----
{PLANTED["private_key"]}
-----END RSA PRIVATE KEY-----"""
HEADERS = {{"Authorization": "Bearer {PLANTED["authorization"]}"}}


def load():
    """Read .env-style lines like the one below."""
    return "AWS_SECRET_ACCESS_KEY={PLANTED["env_line"]}"
'''


@pytest.fixture
def fake():
    with FakeProviderServer() as server:
        yield server


def real_settings(settings, fake, **extra):
    return settings.model_copy(
        update={
            "provider": "openai_compatible",
            "provider_base_url": fake.base_url,
            "provider_model": "fake-model-1",
            "provider_api_key": SecretStr(KEY),
            **extra,
        }
    )


def data_blocks(fake: FakeProviderServer) -> list[str]:
    """The data block of every request (the text that left the machine)."""
    blocks = []
    for request in fake.requests:
        user = request.user_message
        block_id = user.split("<<<DATA ", 1)[1].split("\n", 1)[0]
        blocks.append(user.split(f"<<<DATA {block_id}\n", 1)[1].rsplit(f"\nDATA {block_id}>>>")[0])
    return blocks


def test_scrub_replaces_each_kind_and_counts_it():
    text, counts = scrub(CONFIG_PY)
    for value in PLANTED.values():
        assert value not in text
    assert REDACTED in text
    assert counts == {
        "url_credentials": 1,
        "private_key": 1,
        "authorization": 1,  # the Bearer value...
        "token": 1,
        # API_TOKEN = "...", AWS_SECRET_ACCESS_KEY=..., and "Authorization": "Bearer [REDACTED]"
        # (the whole header value goes, scheme included)
        "assignment": 3,
    }
    assert "def load():" in text and "Settings for the shop service." in text  # code kept


def test_scrub_leaves_ordinary_text_alone():
    # (A name containing a sensitive word is masked even when harmless, e.g. total_tokens=12:
    # over-masking is the safe direction, documented in docs/security.md.)
    text = "Orders are totalled in cents; the author is Ada. See https://example.com/a?b=1."
    assert scrub(text) == (text, {})


def test_repository_secrets_are_replaced_before_sending_and_only_counted(
    tmp_path, small_settings, fake
):
    settings = real_settings(small_settings, fake)
    archive = zip_samples.small_repo(extra=[("src/shop/config.py", CONFIG_PY.encode())])
    stages, workspace, job = make_pipeline(tmp_path, settings, archive)
    run_all(stages, workspace, job)

    sent = "\n".join(data_blocks(fake))
    assert "DATABASE_URL" in sent  # the file was summarized...
    for value in PLANTED.values():
        assert value not in sent  # ...without its secrets
    assert all(r.headers["Authorization"] == f"Bearer {KEY}" for r in fake.requests)

    manifest_text = workspace.read_text(workspace.artifact("manifest.json", job.attempt_no))
    manifest = json.loads(manifest_text)
    # Counted per chunk sent: chunks overlap, so a secret near a chunk border counts twice.
    expected: dict[str, int] = {}
    for chunk in workspace.iter_jsonl(workspace.work("chunks.jsonl")):
        for kind, n in scrub(chunk["text"])[1].items():
            expected[kind] = expected.get(kind, 0) + n
    assert set(expected) == {
        "assignment",
        "authorization",
        "private_key",
        "token",
        "url_credentials",
    }
    assert manifest["secret_scrubbing"] == {
        "replaced": sum(expected.values()),
        "by_kind": dict(sorted(expected.items())),
    }
    for value in PLANTED.values():
        assert value not in manifest_text
    assert KEY not in manifest_text
    assert manifest["summary"]["provider"] == "openai_compatible"
    summary_md = workspace.read_text(workspace.artifact("summary.md", job.attempt_no))
    assert "generated by a language model" in summary_md


def test_pdf_text_is_scrubbed_too(tmp_path, unit_settings, fake):
    settings = real_settings(unit_settings, fake)
    paragraphs = pdf_samples.sample_paragraphs(4)
    paragraphs[1] += f" The staging password={PLANTED['env_line']} must be rotated."
    pdf = pdf_samples.text_pdf([paragraphs[:2], paragraphs[2:]])
    stages, workspace, job = _pdf_pipeline(tmp_path, settings, pdf)
    for stage in stages:
        stage.run(RecordingContext(workspace, job))

    assert all(PLANTED["env_line"] not in block for block in data_blocks(fake))
    manifest = json.loads(workspace.read_text(workspace.artifact("manifest.json", 1)))
    assert manifest["secret_scrubbing"] == {"replaced": 1, "by_kind": {"assignment": 1}}


def test_the_stub_sends_nothing_and_scrubs_nothing(tmp_path, small_settings):
    archive = zip_samples.small_repo(extra=[("src/shop/config.py", CONFIG_PY.encode())])
    stages, workspace, job = make_pipeline(tmp_path, small_settings, archive)
    run_all(stages, workspace, job)
    manifest = json.loads(workspace.read_text(workspace.artifact("manifest.json", 1)))
    assert manifest["secret_scrubbing"] is None
    assert manifest["usage"]["estimated_cost"] is None
    assert manifest["usage"]["prompt_tokens"] is None


def test_usage_and_estimated_cost_reach_the_manifest(tmp_path, unit_settings, fake):
    settings = real_settings(
        unit_settings,
        fake,
        provider_input_price_per_million_tokens=Decimal("0.05"),
        provider_output_price_per_million_tokens=Decimal("0.08"),
    )
    stages, workspace, job = _pdf_pipeline(tmp_path, settings, pdf_samples.report_pdf(6))
    context = RecordingContext(workspace, job)
    for stage in stages:
        stage.run(context)

    calls = len(fake.requests)
    assert calls == len(context.calls) > 1
    assert all(c.usage.total_tokens == USAGE["total_tokens"] for c in context.calls)
    usage = json.loads(workspace.read_text(workspace.artifact("manifest.json", 1)))["usage"]
    prompt, completion = USAGE["prompt_tokens"] * calls, USAGE["completion_tokens"] * calls
    assert usage["provider_calls"] == calls
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (prompt, completion)
    assert usage["total_tokens"] == USAGE["total_tokens"] * calls
    expected = (Decimal(prompt) * Decimal("0.05") + Decimal(completion) * Decimal("0.08")) / (10**6)
    assert usage["estimated_cost"] == {
        "amount": str(expected.quantize(Decimal("0.000001"))),
        "currency": "USD",
        "input_price_per_million_tokens": "0.05",
        "output_price_per_million_tokens": "0.08",
    }


def test_a_retry_resumes_from_cached_summaries(tmp_path, unit_settings, fake):
    stages, workspace, job = _pdf_pipeline(
        tmp_path, real_settings(unit_settings, fake), pdf_samples.report_pdf(6)
    )
    summarize = next(s for s in stages if s.name == "summarize")
    for stage in stages[: stages.index(summarize)]:
        stage.run(RecordingContext(workspace, job))
    fake.reply(Reply(), Reply(status=503))  # the second call fails (transient)
    with pytest.raises(JobError) as failed:
        summarize.run(RecordingContext(workspace, job))
    assert failed.value.error_class == "provider_unavailable" and failed.value.retryable
    first_success = data_blocks(fake)[0]

    fake.requests.clear()
    summarize.run(RecordingContext(workspace, job))
    retried = data_blocks(fake)
    assert first_success not in retried  # already summarized: not sent again
    chunks = sum(1 for _ in workspace.iter_jsonl(workspace.work("chunks.jsonl")))
    summary = workspace.read_json(workspace.work("summary.json"))
    assert summary["calls"] == len(retried) + 1  # the cached one counts in the result

    fake.requests.clear()  # a third run calls nothing at all: chunks and combines are cached
    summarize.run(RecordingContext(workspace, job))
    assert fake.requests == [] and chunks > 1


def test_another_model_never_reuses_cached_summaries(tmp_path, unit_settings, fake):
    stages, workspace, job = _pdf_pipeline(
        tmp_path, real_settings(unit_settings, fake), pdf_samples.report_pdf(6)
    )
    for stage in stages:
        stage.run(RecordingContext(workspace, job))
    summarize = next(s for s in stages if s.name == "summarize")
    first = len(fake.requests)
    summarize.provider = OpenAICompatibleProvider(
        base_url=fake.base_url, api_key=KEY, model="fake-model-2", max_output_tokens=64
    )
    fake.requests.clear()
    summarize.run(RecordingContext(workspace, job))
    assert len(fake.requests) == first  # everything summarized again
    assert {r.body["model"] for r in fake.requests} == {"fake-model-2"}


def test_another_endpoint_or_token_limit_never_reuses_cached_summaries(
    tmp_path, unit_settings, fake
):
    stages, workspace, job = _pdf_pipeline(
        tmp_path, real_settings(unit_settings, fake), pdf_samples.report_pdf(6)
    )
    for stage in stages:
        stage.run(RecordingContext(workspace, job))
    summarize = next(s for s in stages if s.name == "summarize")
    first = len(fake.requests)
    summarize.provider = OpenAICompatibleProvider(  # same model, another output-token limit
        base_url=fake.base_url, api_key=KEY, model=summarize.provider.model, max_output_tokens=65
    )
    fake.requests.clear()
    summarize.run(RecordingContext(workspace, job))
    assert len(fake.requests) == first  # everything summarized again


def test_a_stage_deadline_during_a_pacing_wait_is_not_recorded_as_a_call(
    tmp_path, unit_settings, fake
):
    stages, workspace, job = _pdf_pipeline(
        tmp_path, real_settings(unit_settings, fake), pdf_samples.report_pdf(6)
    )
    summarize = next(s for s in stages if s.name == "summarize")
    for stage in stages[: stages.index(summarize)]:
        stage.run(RecordingContext(workspace, job))

    def deadline_while_waiting(text, options):
        raise JobError("stage_timeout", limit=1)  # what SummaryCaller._sleep raises

    summarize.provider.summarize = deadline_while_waiting
    context = RecordingContext(workspace, job)
    with pytest.raises(JobError) as failed:
        summarize.run(context)
    assert failed.value.error_class == "stage_timeout"
    assert context.calls == [] and fake.requests == []


def _pdf_pipeline(tmp_path, settings, pdf: bytes):
    import hashlib
    import uuid

    from app.domain.enums import UploadKind, WorkflowType
    from app.queue.worker_process import workflow_config
    from app.storage.local import LocalStorage
    from app.storage.workspace import JobWorkspace
    from app.workflows.base import JobInput, UploadInput
    from app.workflows.registry import stages_for

    storage = LocalStorage(tmp_path / "storage")
    upload_id, job_id = uuid.uuid4(), uuid.uuid4()
    upload_path = storage.upload_path(upload_id, UploadKind.PDF)
    target = storage.absolute(upload_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(pdf)
    job = JobInput(
        job_id=job_id,
        attempt_no=1,
        workflow_type=WorkflowType.PDF_INGEST.value,
        upload=UploadInput(
            upload_id=upload_id,
            storage_path=upload_path,
            sha256=hashlib.sha256(pdf).hexdigest(),
            size_bytes=len(pdf),
            original_filename="report.pdf",
        ),
    )
    stages = stages_for(WorkflowType.PDF_INGEST, workflow_config(settings))
    return stages, JobWorkspace(storage, job_id), job
