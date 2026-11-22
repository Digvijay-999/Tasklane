"""OpenTelemetry tracing: setup, the W3C `traceparent` helpers, and a span helper.

A *trace* is the story of one piece of work as a tree of *spans* (timed steps). Every span has the
trace id of its tree and its own span id. A `traceparent` string (W3C Trace Context, e.g.
`00-<32 hex trace id>-<16 hex span id>-01`) is how a span's identity travels between processes:
here through database rows (jobs.traceparent, outbox.traceparent) and Celery message headers. See
docs/observability.md for the propagation design (one trace per job, parent = the span that
caused the work).

Rules every span in this codebase follows (a test scans every span a test run produces):
- attributes are ids, enums, counts and durations: job_id, attempt_no, workflow_type, stage,
  error_class, provider, model, outcome, ...; never document text, file contents, archive file
  names, paths, SQL parameter values, API keys or tokens;
- exceptions are never recorded on spans (their messages and tracebacks can contain any of the
  above): `span()` sets an ERROR status without a description, and the code sets `error_class`.

Setup happens once per process (`configure_telemetry`). By default no exporter is installed:
spans are created (so trace ids exist and appear in logs and the API) and then dropped, and
nothing is sent anywhere. OTLP export is opt-in (OTEL_EXPORTER_OTLP_ENDPOINT) and runs on a
background thread with short timeouts, so a slow or missing collector never blocks a request or
a stage.
"""

import json
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACER_NAME = "tasklane"
# Spans that must not get database child spans (background polls, heartbeats) set this key in
# their context; app/observability/db.py checks it.
SUPPRESS_DB_SPANS = otel_context.create_key("tasklane.suppress_db_spans")

_propagator = TraceContextTextMapPropagator()
_configured = False
_configure_lock = threading.Lock()


def configure_telemetry(
    service_name: str,
    *,
    otlp_endpoint: str | None = None,
    export_timeout_seconds: float = 2.0,
    span_file: Path | None = None,
    disabled: bool = False,
    extra_processors: Sequence[SpanProcessor] = (),
) -> bool:
    """Install the tracer provider for this process. Only the first call has an effect
    (OpenTelemetry allows one global provider per process); returns True if it installed it."""
    global _configured
    with _configure_lock:
        if _configured or disabled:
            return False
        _configured = True
        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
        if otlp_endpoint:
            # Imported here: the exporter pulls in protobuf and requests, unneeded by default.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(
                endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces", timeout=export_timeout_seconds
            )
            # The batch processor queues finished spans and exports them from its own thread, so
            # a dead collector costs a full queue (spans dropped), never a blocked caller.
            provider.add_span_processor(
                BatchSpanProcessor(
                    exporter,
                    max_queue_size=2048,
                    schedule_delay_millis=1000,
                    max_export_batch_size=512,
                    export_timeout_millis=int(export_timeout_seconds * 1000),
                )
            )
        if span_file is not None:
            provider.add_span_processor(SimpleSpanProcessor(JsonLinesSpanExporter(span_file)))
        for processor in extra_processors:
            provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        return True


def configure_from_settings(settings: Any, service_name: str) -> bool:
    return configure_telemetry(
        service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        export_timeout_seconds=settings.otel_export_timeout_seconds,
        span_file=settings.telemetry_span_file,
        disabled=settings.otel_sdk_disabled,
    )


def flush_telemetry(timeout_seconds: float = 2.0) -> None:
    """Export spans still queued. Celery's child processes end with os._exit(), which skips the
    SDK's own exit handler, so the worker calls this when a child shuts down."""
    flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    if flush is not None:
        flush(int(timeout_seconds * 1000))


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


# --- traceparent ------------------------------------------------------------------------------


def current_traceparent() -> str | None:
    """The W3C traceparent of the current span, or None if there is no valid span (telemetry
    disabled, or called outside any span)."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> Context:
    """A context whose parent is the span named by `traceparent`, or an empty context (the next
    span starts a new trace) if it is missing or malformed. Never the *current* context: a span
    continued from a stored traceparent must not accidentally hang under whatever is running."""
    if not traceparent:
        return Context()
    return _propagator.extract({"traceparent": traceparent}, context=Context())


def link_to_current() -> list[Link]:
    """A link to the current span, if there is one (e.g. from a job's span back to the HTTP
    request that caused it, which lives in a different trace)."""
    span_context = trace.get_current_span().get_span_context()
    return [Link(span_context)] if span_context.is_valid else []


# --- spans ------------------------------------------------------------------------------------


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    context: Context | None = None,
    attributes: Mapping[str, Any] | None = None,
    links: Sequence[Link] = (),
) -> Iterator[Span]:
    """Start a span and make it current. `context=None` continues the current trace; pass
    context_from_traceparent(...) to continue a stored one. An exception marks the span as an
    ERROR (no description, no recorded exception: see the module docstring) and propagates."""
    with tracer().start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=clean_attributes(attributes),
        links=links,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException:
            set_error(current)
            raise


def set_error(target: Span, error_class: str | None = None) -> None:
    """Mark a span as failed, with the error class (never an exception message) if known."""
    if error_class:
        target.set_attribute("error_class", error_class)
    target.set_status(Status(StatusCode.ERROR))


def clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop None values (OpenTelemetry rejects them) and turn enums/UUIDs into plain strings."""
    cleaned: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        cleaned[key] = value if isinstance(value, (bool, int, float, str)) else str(value)
    return cleaned


@contextmanager
def suppressed_db_spans() -> Iterator[None]:
    """Run a block without database child spans (still inside the current trace, so log lines
    keep their trace id). Used for polls and heartbeats, which would otherwise add a span every
    few seconds."""
    token = otel_context.attach(otel_context.set_value(SUPPRESS_DB_SPANS, True))
    try:
        yield
    finally:
        otel_context.detach(token)


# --- A file exporter for tests and debugging ----------------------------------------------------


class JsonLinesSpanExporter(SpanExporter):
    """Appends each finished span as one JSON line to a file (TELEMETRY_SPAN_FILE). Every write is
    one O_APPEND system call, so several processes (the Celery worker's children) can share the
    file without interleaving lines. For tests and local debugging only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = "".join(json.dumps(span_record(s), separators=(",", ":")) + "\n" for s in spans)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, lines.encode())
        finally:
            os.close(fd)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def span_record(s: ReadableSpan) -> dict[str, Any]:
    """A finished span as a plain dict (the format of TELEMETRY_SPAN_FILE)."""
    ctx = s.get_span_context()
    return {
        "name": s.name,
        "kind": s.kind.name,
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
        "parent_span_id": f"{s.parent.span_id:016x}" if s.parent else None,
        "service": s.resource.attributes.get(SERVICE_NAME),
        "status": s.status.status_code.name,
        "status_description": s.status.description,
        "start_ns": s.start_time,
        "end_ns": s.end_time,
        "attributes": dict(s.attributes or {}),
        "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in s.events],
        "links": [
            {"trace_id": f"{link.context.trace_id:032x}", "span_id": f"{link.context.span_id:016x}"}
            for link in s.links
        ],
    }
