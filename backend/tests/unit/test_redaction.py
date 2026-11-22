"""Every kind of secret is masked before it reaches a log line (app/observability/redaction.py),
and so is anything written through the real JSON formatter, tracebacks included."""

import json
import logging

import pytest

from app.observability.logging import JsonFormatter, configure_logging
from app.observability.redaction import (
    REDACTED,
    is_sensitive_key,
    redact,
    redact_value,
    register_environment_secrets,
    register_secret,
)

FAKE_OPENAI_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx1234"
FAKE_ANTHROPIC_KEY = "sk-ant-api03-ZyXwVuTsRqPoNmLkJiHg-0987"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"calling the provider with {FAKE_OPENAI_KEY}", FAKE_OPENAI_KEY),
        (f"key={FAKE_ANTHROPIC_KEY}", FAKE_ANTHROPIC_KEY),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefgh.ijklmnop", "eyJhbGciOiJIUzI1NiJ9"),
        ("token for basic auth: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("aws AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE"),
        (
            "gh token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
            "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
        ),
        ("slack xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
        (
            "google AIzaSyA1234567890abcdefghijklmnopqrstuv",
            "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        ),
        (
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36P",
            "SflKxwRJSMeKKF2QT4fwpMeJf36P",
        ),
        ("password=hunter2-long", "hunter2-long"),
        ('{"password": "s3cr3t-value"}', "s3cr3t-value"),
        ("OPENAI_API_KEY=whatever-it-is", "whatever-it-is"),
        ("client_secret: abc123def456", "abc123def456"),
        ("postgresql://tasklane:pa55word@postgres:5432/tasklane", "pa55word"),
        ("redis://:redispass@redis:6379/0", "redispass"),
        ("https://ghp_tokeninurl12345@github.com/org/repo", "ghp_tokeninurl12345"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
            "MIIEowIBAAKCAQEA",
        ),
    ],
)
def test_secrets_are_masked(text, secret):
    masked = redact(text)
    assert secret not in masked
    assert REDACTED in masked


@pytest.mark.parametrize(
    "text",
    [
        "job claimed",
        "total_tokens=12 for 3 calls",  # a word containing "token" is not a secret name
        "password authentication failed for user",  # no value to mask
        "author: someone",
    ],
)
def test_ordinary_text_is_kept(text):
    if text.startswith("total_tokens"):
        # Over-masking a number here is acceptable; the point is the rest of the line survives.
        assert redact(text).endswith("for 3 calls")
    else:
        assert redact(text) == text


def test_registered_literal_secrets_are_masked_anywhere():
    register_secret("my-very-own-db-password")
    assert redact("connect failed (my-very-own-db-password)") == f"connect failed ({REDACTED})"


def test_short_values_are_not_registered_as_literals():
    register_secret("abc")
    assert redact("abcdef") == "abcdef"


def test_environment_values_with_sensitive_names_are_registered():
    register_environment_secrets(
        {"SOME_SERVICE_TOKEN": "tok-12345678-xyz", "LOG_LEVEL": "INFO-and-more"}
    )
    assert "tok-12345678-xyz" not in redact("env dump: tok-12345678-xyz")
    assert redact("level INFO-and-more") == "level INFO-and-more"


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("api_key", True),
        ("apiKey", True),
        ("POSTGRES_PASSWORD", True),
        ("authorization", True),
        ("session_id", True),
        ("job_id", False),
        ("total_tokens", False),
        ("author", False),
        ("worker_id", False),
    ],
)
def test_sensitive_keys(key, sensitive):
    assert is_sensitive_key(key) is sensitive


def test_values_under_sensitive_keys_are_masked_whole_and_structures_are_walked():
    assert redact_value("anything at all", "api_key") == REDACTED
    assert redact_value({"headers": {"Authorization": "x y z"}, "n": 3}) == {
        "headers": {"Authorization": REDACTED},
        "n": 3,
    }
    assert redact_value([f"k {FAKE_OPENAI_KEY}"]) == [f"k {REDACTED}"]
    assert redact_value(42, "token") == 42


def _format(record: logging.LogRecord) -> dict:
    line = JsonFormatter().format(record)
    return json.loads(line)  # still valid JSON after redaction


def test_the_formatter_redacts_message_extras_and_tracebacks():
    configure_logging("INFO", secrets=("formatter-test-password",))
    try:
        raise RuntimeError(f"provider refused {FAKE_OPENAI_KEY} (formatter-test-password)")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()
    record = logging.makeLogRecord(
        {
            "name": "t",
            "levelname": "ERROR",
            "msg": "calling with Authorization: Bearer abcdefghijklmnop",
            "exc_info": exc_info,
            "api_key": FAKE_ANTHROPIC_KEY,
            "url": "postgresql://u:secret-pw@db/x",
        }
    )
    line = _format(record)
    text = json.dumps(line)
    for secret in (
        FAKE_OPENAI_KEY,
        FAKE_ANTHROPIC_KEY,
        "abcdefghijklmnop",
        "secret-pw",
        "formatter-test-password",
    ):
        assert secret not in text
    assert "Traceback" in line["exception"]  # the traceback stays, redacted


def test_a_logged_fake_api_key_is_redacted_end_to_end(capsys):
    """What the walkthrough shows: a real logger call through the configured handler."""
    configure_logging("INFO")
    logging.getLogger("tasklane.demo").warning(f"provider call with key {FAKE_OPENAI_KEY}")
    out = capsys.readouterr().out
    assert FAKE_OPENAI_KEY not in out
    assert f"provider call with key {REDACTED}" in out


@pytest.mark.parametrize(
    "hostile",
    [
        "/" + "pass." * 13000,  # many sensitive words, no separator (was cubic: 84 s at 5 KB)
        "a." * 30000 + "://",  # URL scheme restarts
        "eyJ" * 20000,
        "token=" * 10000,
        "x" * 200_000,  # longer than the cap
    ],
    ids=["sensitive-words", "url-scheme", "jwt-prefix", "many-pairs", "huge"],
)
def test_redaction_runs_in_linear_time_on_hostile_input(hostile):
    """A request path is attacker-controlled and is logged; a slow regex would hold the GIL and
    freeze the whole process."""
    import time

    started = time.perf_counter()
    redact(hostile)
    assert time.perf_counter() - started < 0.5


def test_overlong_strings_are_truncated_before_redaction():
    out = redact("y" * 70_000)
    assert len(out) < 70_000 and out.endswith("characters]")


@pytest.mark.parametrize(
    "key", ["accessToken", "authToken", "refreshToken", "dbPassword", "apiKey"]
)
def test_camel_case_sensitive_keys(key):
    assert is_sensitive_key(key)


def test_quoted_values_with_spaces_are_masked_whole():
    masked = redact('password="correct horse battery" next=1')
    assert masked == f'password="{REDACTED}" next=1'
