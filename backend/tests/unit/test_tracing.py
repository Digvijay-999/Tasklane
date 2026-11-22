"""Tracing helpers, log correlation, and that a dead collector never blocks anyone."""

import json
import logging
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.domain.tracing import trace_id_from_traceparent
from app.observability.logging import ContextFilter, JsonFormatter, log_context
from app.observability.tracing import (
    JsonLinesSpanExporter,
    context_from_traceparent,
    current_traceparent,
    span,
)
from tests import telemetry

VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (VALID, "0af7651916cd43dd8448eb211c80319c"),
        (None, None),
        ("", None),
        ("00-00000000000000000000000000000000-b7ad6b7169203331-01", None),
        ("01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", None),
        ("00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01", None),
        (VALID + "\n", None),
    ],
)
def test_trace_id_from_traceparent(value, expected):
    assert trace_id_from_traceparent(value) == expected


def test_a_span_continued_from_a_traceparent_joins_that_trace():
    with span("child", context=context_from_traceparent(VALID)) as child:
        assert f"{child.get_span_context().trace_id:032x}" == "0af7651916cd43dd8448eb211c80319c"
        assert current_traceparent().split("-")[1] == "0af7651916cd43dd8448eb211c80319c"
    [record] = [r for r in telemetry.captured() if r["name"] == "child"]
    assert record["parent_span_id"] == "b7ad6b7169203331"


def test_a_missing_or_bad_traceparent_starts_a_new_trace_not_the_current_one():
    with span("outer") as outer:
        for value in (None, "garbage"):
            with span("fresh", context=context_from_traceparent(value)) as fresh:
                assert fresh.get_span_context().trace_id != outer.get_span_context().trace_id


def test_no_traceparent_outside_a_span():
    assert current_traceparent() is None


def test_exceptions_mark_the_span_failed_without_recording_the_message():
    with pytest.raises(ValueError), span("failing"):
        raise ValueError("secret file content: Lorem ipsum")
    [record] = [r for r in telemetry.captured() if r["name"] == "failing"]
    assert record["status"] == "ERROR"
    assert record["status_description"] is None
    assert record["events"] == []  # no exception event (message, stacktrace)


def test_log_lines_carry_trace_and_span_ids_and_bound_fields():
    record = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": "hello"})
    with span("logging") as current, log_context(job_id="j-1", attempt_no=2, worker_id=None):
        ContextFilter().filter(record)
        context = current.get_span_context()
    line = json.loads(JsonFormatter().format(record))
    assert line["trace_id"] == f"{context.trace_id:032x}"
    assert line["span_id"] == f"{context.span_id:016x}"
    assert line["job_id"] == "j-1" and line["attempt_no"] == 2
    assert "worker_id" not in line  # None values are not bound


def test_an_explicit_extra_wins_over_a_bound_field():
    record = logging.makeLogRecord({"name": "t", "msg": "x", "job_id": "explicit"})
    with log_context(job_id="bound"):
        ContextFilter().filter(record)
    assert record.job_id == "explicit"


def test_no_trace_ids_outside_a_span():
    record = logging.makeLogRecord({"name": "t", "msg": "x"})
    ContextFilter().filter(record)
    assert "trace_id" not in json.loads(JsonFormatter().format(record))


def test_span_file_exporter_writes_one_json_line_per_span(tmp_path):
    provider = TracerProvider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    path = tmp_path / "spans.jsonl"
    provider.add_span_processor(SimpleSpanProcessor(JsonLinesSpanExporter(path)))
    with provider.get_tracer("t").start_as_current_span("a", attributes={"job_id": "1"}):
        with provider.get_tracer("t").start_as_current_span("b"):
            pass
    records = telemetry.read_span_file(path)
    assert [r["name"] for r in records] == ["b", "a"]
    assert records[0]["parent_span_id"] == records[1]["span_id"]
    assert records[1]["attributes"] == {"job_id": "1"}


def test_an_unreachable_collector_never_blocks_span_creation():
    """192.0.2.1 is reserved for documentation (TEST-NET-1): connections to it hang until the
    timeout. Ending spans must still be instant, and shutting down bounded by the timeout."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint="http://192.0.2.1:4318/v1/traces", timeout=1),
            schedule_delay_millis=50,
            export_timeout_millis=1000,
        )
    )
    tracer = provider.get_tracer("t")
    started = time.monotonic()
    for _ in range(500):
        with tracer.start_as_current_span("work"):
            pass
    assert time.monotonic() - started < 1.0
    started = time.monotonic()
    provider.shutdown()
    assert time.monotonic() - started < 10


def test_the_global_provider_is_the_sdk_one():
    assert type(trace.get_tracer_provider()).__name__ == "TracerProvider"
