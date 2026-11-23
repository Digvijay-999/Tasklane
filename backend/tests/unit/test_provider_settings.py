"""Provider settings are validated at startup, the worker refuses to start without a key,
Retry-After never exceeds the backoff maximum, and the configured key never reaches a log line."""

import io
import json
import logging
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.domain.costs import Pricing, cost_record, estimate_cost
from app.domain.retry import RetryPolicy
from app.observability.logging import JsonFormatter
from app.observability.redaction import REDACTED, register_secret
from app.providers.factory import create_provider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.stub import StubProvider

BACKEND = Path(__file__).resolve().parents[2]
REAL = {
    "provider": "openai_compatible",
    "provider_base_url": "https://api.groq.com/openai/v1",
    "provider_model": "llama-3.1-8b-instant",
}


def settings(**values) -> Settings:
    base = {
        "postgres_user": "u",
        "postgres_password": "password-for-tests",
        "postgres_db": "d",
    }
    return Settings(**{**base, **values})


def test_the_stub_is_the_default_and_needs_nothing():
    default = settings()
    assert default.provider == "stub"
    assert default.provider_key_problem() is None
    assert isinstance(create_provider(default), StubProvider)
    assert default.provider_pricing is None


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ({"provider_base_url": None}, "PROVIDER_BASE_URL"),
        ({"provider_base_url": "api.groq.com/openai/v1"}, "PROVIDER_BASE_URL"),
        ({"provider_model": None}, "PROVIDER_MODEL"),
    ],
)
def test_a_real_provider_needs_a_base_url_and_a_model(missing, message):
    with pytest.raises(ValidationError, match=message):
        settings(**{**REAL, **missing})


def test_a_real_provider_without_a_key_is_refused_by_the_worker():
    real = settings(**REAL)  # the API and dispatcher don't hold the key: fine for them
    assert "PROVIDER_API_KEY" in real.provider_key_problem()
    assert "PROVIDER_API_KEY" in settings(**REAL, provider_api_key="   ").provider_key_problem()
    with pytest.raises(ValueError, match="PROVIDER_API_KEY"):
        create_provider(real)
    keyed = settings(**REAL, provider_api_key="a-key-that-is-long-enough")
    assert keyed.provider_key_problem() is None
    assert isinstance(create_provider(keyed), OpenAICompatibleProvider)


def test_the_worker_process_stops_at_startup_with_a_clear_message():
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper()}
    env.update(
        PROVIDER="openai_compatible",
        PROVIDER_BASE_URL="https://api.groq.com/openai/v1",
        PROVIDER_MODEL="llama-3.1-8b-instant",
        POSTGRES_USER="u",
        POSTGRES_PASSWORD="p",
        POSTGRES_DB="d",
    )
    result = subprocess.run(
        [sys.executable, "-c", "import app.queue.worker_main"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "tasklane worker cannot start" in result.stderr
    assert "PROVIDER_API_KEY" in result.stderr


def test_the_provider_timeout_must_stay_within_half_the_lease():
    with pytest.raises(ValidationError, match="half of LEASE_SECONDS"):
        settings(**REAL, lease_seconds=30, provider_timeout_seconds=20)


def test_prices_come_in_pairs():
    with pytest.raises(ValidationError, match="PRICE"):
        settings(provider_input_price_per_million_tokens="0.05")
    priced = settings(
        provider_input_price_per_million_tokens="0.05",
        provider_output_price_per_million_tokens="0.08",
    )
    assert priced.provider_pricing == Pricing(Decimal("0.05"), Decimal("0.08"))


def test_cost_estimates_need_prices_and_both_token_counts():
    pricing = Pricing(Decimal("0.05"), Decimal("0.08"))
    assert estimate_cost(pricing, 1_000_000, 500_000) == Decimal("0.090000")
    assert estimate_cost(pricing, 123, 45) == Decimal("0.000010")  # rounded to 6 places
    assert estimate_cost(None, 100, 100) is None
    assert estimate_cost(pricing, None, 100) is None
    assert cost_record(pricing, 2_000_000, 0) == {
        "amount": "0.100000",
        "currency": "USD",
        "input_price_per_million_tokens": "0.05",
        "output_price_per_million_tokens": "0.08",
    }


def test_retry_after_is_respected_but_capped_by_the_backoff_maximum():
    policy = RetryPolicy(base_seconds=5, max_seconds=300, random=lambda: 0.0)  # backoff 2.5 s
    assert policy.delay_seconds(1) == 2.5
    assert policy.delay_seconds(1, retry_after=40) == 40
    assert policy.delay_seconds(1, retry_after=1) == 2.5  # never shorter than the backoff
    assert policy.delay_seconds(1, retry_after=10_000) == 300  # never longer than the maximum


def _log_line(message: str, **extra) -> str:
    record = logging.LogRecord("app.test", logging.WARNING, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return JsonFormatter().format(record)


def test_the_configured_key_and_authorization_headers_never_reach_a_log_line():
    key = "gsk_ConfiguredProviderKey0123456789"  # no well-known format: only the literal catches it
    register_secret(key)
    lines = [
        _log_line(f"calling the provider with {key}"),
        _log_line("request headers", headers={"Authorization": f"Bearer {key}"}),
        _log_line(f"Authorization: Bearer {key}"),
        _log_line("httpx", detail=f"authorization=Bearer {key}"),
        _log_line("settings", provider_api_key=key),
        _log_line(repr(SecretStr(key))),
    ]
    for line in lines:
        assert key not in line, line
    assert REDACTED in lines[0] and REDACTED in lines[2]
    assert json.loads(lines[4])["provider_api_key"] == REDACTED


def test_an_unregistered_bearer_token_is_masked_by_its_format():
    stream = io.StringIO(_log_line("Authorization: Bearer abc.def-unregistered-token"))
    assert "unregistered-token" not in stream.getvalue()
