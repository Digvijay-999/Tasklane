"""Span capture and the "no forbidden attributes" scan, shared by every test.

The root conftest installs the tracer provider for the test process (first call wins) with an
in-memory exporter, and scans every span each test produced. The e2e tests point their
subprocesses at a JSON-lines span file (TELEMETRY_SPAN_FILE) and scan that too.

The scan is an allowlist: a span attribute whose key isn't listed here fails the test, so a new
attribute must be added here on purpose (and documented in docs/observability.md).
"""

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.observability.tracing import configure_telemetry, span_record

SPANS = InMemorySpanExporter()

ALLOWED_SPAN_ATTRIBUTES = frozenset(
    {
        # job identity and classification (docs/observability.md, "Span attributes")
        "job_id",
        "attempt_no",
        "workflow_type",
        "stage",
        "error_class",
        "provider",
        "model",
        "outcome",
        "trigger",
        "drop_reason",
        "purpose",
        "queue_wait_seconds",
        "dispatch_attempt",
        "usage.prompt_tokens",
        "usage.completion_tokens",
        "usage.total_tokens",
        # event streams
        "resume_after",
        "events_sent",
        "last_seq",
        # HTTP (route template only, never the URL or query string)
        "http.request.method",
        "http.route",
        "http.response.status_code",
        # messaging and errors (type names only)
        "messaging.system",
        "error.type",
        # database (statement text with placeholders, never parameter values)
        "db.system",
        "db.operation",
        "db.statement",
        "db.response.status_code",
    }
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Anything that looks like a path, a secret or file content must never be an attribute value.
FORBIDDEN_VALUE_PARTS = (
    "/data/storage",
    "/tmp/",
    "pytest-of",
    "uploads/",
    "work/",
    "artifacts/",
    "Bearer ",
    "sk-",
    "password",
    "unit-test-secret",  # tests/conftest.py TEST_PASSWORD
    "Traceback",
)


def install() -> None:
    configure_telemetry("tasklane-test", extra_processors=[SimpleSpanProcessor(SPANS)])


def captured() -> list[dict[str, Any]]:
    return [span_record(s) for s in SPANS.get_finished_spans()]


def read_span_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def span_problems(records: Iterable[dict[str, Any]], extra_forbidden: Iterable[str] = ()) -> list:
    """Every forbidden attribute key, forbidden value, parameter-like value in a database
    statement, or recorded exception, as (span name, key, value) tuples."""
    forbidden = tuple(FORBIDDEN_VALUE_PARTS) + tuple(extra_forbidden)
    problems = []
    for record in records:
        name = record["name"]
        for key, value in record["attributes"].items():
            if key not in ALLOWED_SPAN_ATTRIBUTES:
                problems.append((name, key, value))
                continue
            text = str(value)
            if key == "db.statement":
                # Parameters travel separately; an id inlined into SQL would be a leaked value.
                if _UUID.search(text):
                    problems.append((name, key, value))
                continue
            if key == "http.route":  # a template from our code, e.g. /v1/jobs/{job_id}/...
                continue
            if any(part in text for part in forbidden):
                problems.append((name, key, value))
        for event in record["events"]:  # exceptions are never recorded on spans
            problems.append((name, f"event:{event['name']}", event["attributes"]))
        if record.get("status_description"):
            problems.append((name, "status_description", record["status_description"]))
    return problems
