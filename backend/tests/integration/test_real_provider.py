"""The OpenAI-compatible provider through the execution service and the API (real database, real
PDF stages, fake HTTP provider): usage per call, the job detail's usage and estimated cost,
GET /v1/meta, Retry-After, a wrong key failing at once, and a malformed reply."""

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProviderCall
from app.db.repositories.provider_calls import insert_provider_call
from app.domain.retry import RetryPolicy
from app.main import create_app
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.services.execution import RunOutcome
from tests.factories import make_job
from tests.fake_provider import USAGE, FakeProviderServer, Reply
from tests.integration import test_retries as retry_tests

api_settings = retry_tests.api_settings
worker = retry_tests.worker
submit, load, run_until_finished = (
    retry_tests.submit,
    retry_tests.load,
    retry_tests.run_until_finished,
)

KEY = "fake-integration-key-0123456789abcdef"
PRICES = {
    "provider_input_price_per_million_tokens": Decimal("0.05"),
    "provider_output_price_per_million_tokens": Decimal("0.08"),
}


@pytest.fixture
def fake():
    with FakeProviderServer() as server:
        yield server


@pytest.fixture
def real_settings(api_settings, fake):
    return api_settings.model_copy(
        update={
            "provider": "openai_compatible",
            "provider_base_url": fake.base_url,
            "provider_model": "fake-model-1",
            **PRICES,
        }
    )


@pytest.fixture
def real_api(engine, real_settings):
    """The API as configured for a real provider (it never receives the key)."""
    with TestClient(create_app(real_settings)) as client:
        yield client


def provider(fake, model="fake-model-1") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url=fake.base_url, api_key=KEY, model=model, max_output_tokens=128
    )


def test_meta_says_document_text_goes_to_the_real_provider(real_api, api):
    remote = real_api.get("/v1/meta").json()["provider"]
    assert remote == {
        "name": "openai_compatible",
        "model": "fake-model-1",
        "remote": True,
        "host": "127.0.0.1",
        "estimated_cost": True,
    }
    stub = api.get("/v1/meta").json()["provider"]
    assert stub == {
        "name": "stub",
        "model": "stub-extractive-v1",
        "remote": False,
        "host": None,
        "estimated_cost": False,
    }


def test_a_job_records_usage_and_shows_tokens_and_estimated_cost(
    real_api,
    engine,
    worker,
    fake,
):
    job_id = submit(real_api)
    # The worker's settings carry the prices too (for the manifest).
    assert worker(job_id, provider=provider(fake), **PRICES).outcome == RunOutcome.SUCCEEDED

    with Session(engine) as session:
        calls = session.scalars(
            select(ProviderCall).where(ProviderCall.job_id == uuid.UUID(job_id))
        ).all()
    assert len(calls) == len(fake.requests) > 1
    for call in calls:
        assert (call.provider, call.model, call.success) == (
            "openai_compatible",
            "fake-model-1",
            True,
        )
        assert (call.prompt_tokens, call.completion_tokens, call.total_tokens) == (
            USAGE["prompt_tokens"],
            USAGE["completion_tokens"],
            USAGE["total_tokens"],
        )
        assert call.latency_ms >= 0

    detail = real_api.get(f"/v1/jobs/{job_id}").json()
    n = len(calls)
    prompt, completion = USAGE["prompt_tokens"] * n, USAGE["completion_tokens"] * n
    cost = (Decimal(prompt) * Decimal("0.05") + Decimal(completion) * Decimal("0.08")) / 10**6
    assert detail["usage"] == [
        {
            "provider": "openai_compatible",
            "model": "fake-model-1",
            "calls": n,
            "failed_calls": 0,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": USAGE["total_tokens"] * n,
            "estimated_cost": str(cost.quantize(Decimal("0.000001"))),
            "estimated_cost_currency": "USD",
        }
    ]
    manifest = next(a for a in detail["artifacts"] if a["artifact_type"] == "manifest")
    usage = real_api.get(manifest["download_url"]).json()["usage"]
    assert usage["estimated_cost"]["amount"] == detail["usage"][0]["estimated_cost"]


def test_usage_rows_group_by_model_and_price_only_the_configured_one(real_api, engine):
    with Session(engine) as session, session.begin():
        job = make_job(session)
        for model, tokens in (("old-model", 10), ("fake-model-1", 100), ("fake-model-1", None)):
            insert_provider_call(
                session,
                job_id=job.id,
                attempt_no=1,
                stage="summarize",
                purpose="chunk",
                provider="openai_compatible",
                model=model,
                latency_ms=5,
                success=tokens is not None,
                input_chars=10,
                error_class=None if tokens else "provider_unavailable",
                prompt_tokens=tokens,
                completion_tokens=tokens,
                total_tokens=tokens and tokens * 2,
            )
        job_id = job.id
    usage = real_api.get(f"/v1/jobs/{job_id}").json()["usage"]
    assert [(u["model"], u["calls"], u["failed_calls"]) for u in usage] == [
        ("old-model", 1, 0),
        ("fake-model-1", 2, 1),
    ]
    assert usage[0]["estimated_cost"] is None  # another model: no price applies
    assert usage[1]["prompt_tokens"] == 100 and usage[1]["estimated_cost"] == "0.000013"


def test_a_job_without_prices_shows_tokens_but_no_cost(api, engine):
    with Session(engine) as session, session.begin():
        job = make_job(session)
        insert_provider_call(
            session,
            job_id=job.id,
            attempt_no=1,
            stage="summarize",
            purpose="chunk",
            provider="stub",
            model="stub-extractive-v1",
            latency_ms=1,
            success=True,
            input_chars=10,
        )
        job_id = job.id
    [usage] = api.get(f"/v1/jobs/{job_id}").json()["usage"]
    assert usage["calls"] == 1 and usage["prompt_tokens"] is None
    assert usage["estimated_cost"] is None and usage["estimated_cost_currency"] is None


def test_rate_limiting_waits_at_least_retry_after_capped_by_the_maximum(
    real_api,
    engine,
    worker,
    fake,
):
    job_id = submit(real_api)
    fake.reply(Reply(status=429, headers={"Retry-After": "40"}))
    policy = RetryPolicy(base_seconds=0.001, max_seconds=300)
    assert worker(job_id, provider=provider(fake), policy=policy).outcome == (
        RunOutcome.RETRY_SCHEDULED
    )
    job, attempts, events, _ = load(engine, job_id)
    assert attempts[0].error_class == "provider_rate_limited"
    [scheduled] = [e for e in events if e.payload.get("to") == "retry_wait"]
    assert scheduled.payload["retry_in_seconds"] == 40

    capped = submit(real_api)
    fake.reply(Reply(status=429, headers={"Retry-After": "3600"}))
    worker(capped, provider=provider(fake), policy=RetryPolicy(base_seconds=0.001, max_seconds=12))
    _, _, events, _ = load(engine, capped)
    [scheduled] = [e for e in events if e.payload.get("to") == "retry_wait"]
    assert scheduled.payload["retry_in_seconds"] == 12


def test_a_wrong_key_fails_at_once_with_provider_auth_and_no_retry(
    real_api,
    engine,
    worker,
    fake,
):
    job_id = submit(real_api)
    fake.reply(Reply(status=401, body={"error": {"code": "invalid_api_key", "message": KEY}}))
    assert run_until_finished(worker, job_id, provider=provider(fake)) == [RunOutcome.FAILED]

    detail = real_api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "failed" and detail["error_class"] == "provider_auth"
    assert [a["outcome"] for a in detail["attempts"]] == ["failed_permanent"]
    assert "PROVIDER_API_KEY" in detail["error_summary"]
    assert detail["can_retry"] is True  # fix the key, then retry: not an input error
    assert len(fake.requests) == 1
    assert KEY not in real_api.get(f"/v1/jobs/{job_id}").text
    assert KEY not in real_api.get(f"/v1/jobs/{job_id}/events/history").text


def test_a_malformed_reply_fails_permanently(real_api, worker, fake):
    job_id = submit(real_api)
    fake.reply(Reply(body=b"<html>gateway page</html>"))
    assert run_until_finished(worker, job_id, provider=provider(fake)) == [RunOutcome.FAILED]
    detail = real_api.get(f"/v1/jobs/{job_id}").json()
    assert detail["error_class"] == "provider_bad_response"
    assert detail["usage"][0]["failed_calls"] == 1


def test_a_transient_error_retries_and_reuses_the_summaries_already_made(
    real_api,
    engine,
    worker,
    fake,
):
    job_id = submit(real_api)
    fake.reply(Reply(), Reply(status=503))
    outcomes = run_until_finished(worker, job_id, provider=provider(fake))
    assert outcomes == [RunOutcome.RETRY_SCHEDULED, RunOutcome.SUCCEEDED]
    with Session(engine) as session:
        chunk_calls = session.execute(
            select(ProviderCall.attempt_no, ProviderCall.chunk_id, ProviderCall.success).where(
                ProviderCall.job_id == uuid.UUID(job_id), ProviderCall.purpose == "chunk"
            )
        ).all()
    first_ok = {c.chunk_id for c in chunk_calls if c.attempt_no == 1 and c.success}
    second = [c.chunk_id for c in chunk_calls if c.attempt_no == 2]
    assert len(first_ok) == 1 and first_ok.isdisjoint(second)


def test_the_api_process_never_holds_the_key(real_settings):
    # Compose passes PROVIDER_API_KEY to the worker only; the API's settings work without it.
    assert real_settings.provider_api_key is None
    keyed = real_settings.model_copy(update={"provider_api_key": SecretStr(KEY)})
    assert KEY not in repr(keyed) and KEY not in str(keyed.model_dump())
