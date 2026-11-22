"""Structured (JSON) logging: one JSON object per line, with context and redaction.

Every line from the API, dispatcher, worker and recovery loop carries, when they apply:
- `request_id` (API requests; app/observability/http.py sets it);
- `trace_id` and `span_id` of the active OpenTelemetry span, so a line can be found from a trace
  and a trace from a line (docs/observability.md, "Investigating a failed job");
- `job_id`, `attempt_no`, `worker_id`, bound for a block of code with `log_context(...)` (the
  worker binds them for a whole attempt, so even log lines deep inside a stage carry them).

Every string in a line (message, extra fields, traceback) is redacted first (redaction.py):
API keys, bearer tokens, passwords, connection-string credentials and registered secret values
are masked. Tracebacks stay in logs only, never in API responses or events.
"""

import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from app.observability.redaction import (
    redact,
    redact_value,
    register_environment_secrets,
    register_secret,
)

_HANDLER_NAME = "tasklane-json"

# A ContextVar is like Node's AsyncLocalStorage: each request (or job attempt) sees its own value,
# even when many run concurrently.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_log_fields: ContextVar[dict[str, Any]] = ContextVar("log_fields", default={})  # noqa: B039 (never mutated)

# Attributes every LogRecord has; anything else was passed via `extra=` and is included in output.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "request_id",
    "trace_id",
    "span_id",
}
# Uvicorn attaches a terminal-colored copy of some messages; it is noise in JSON.
_IGNORED_ATTRS = {"color_message"}


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add these fields to every log line written inside the block (by any code, in this thread
    or async task). None values are ignored; an explicit `extra=` value wins."""
    added = {key: value for key, value in fields.items() if value is not None}
    token = _log_fields.set({**_log_fields.get(), **added})
    try:
        yield
    finally:
        _log_fields.reset(token)


class ContextFilter(logging.Filter):
    """Copies the request id, the active span's ids and the bound log fields onto each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = f"{span_context.trace_id:032x}"
            record.span_id = f"{span_context.span_id:016x}"
        for key, value in _log_fields.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class TelemetryErrorRateLimit(logging.Filter):
    """At most one line per minute per message from the OpenTelemetry SDK itself. A stopped
    collector makes the exporter warn on every batch (each second); one line a minute says the
    same without flooding the logs."""

    def __init__(self, interval_seconds: float = 60.0) -> None:
        super().__init__()
        self.interval = interval_seconds
        self._last: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("opentelemetry"):
            return True
        key = (record.name, str(record.msg))
        now = time.monotonic()
        with self._lock:
            if now - self._last.get(key, -self.interval) < self.interval:
                return False
            self._last[key] = now
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("request_id", "trace_id", "span_id"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in _IGNORED_ATTRS and not key.startswith("_"):
                payload[key] = redact_value(value, key)
        if record.exc_info:
            # Tracebacks stay in logs (never in responses or events), and are redacted too.
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", secrets: tuple[str | None, ...] = ()) -> None:
    """Send all logs (ours, uvicorn's, Celery's) to stdout as JSON. `secrets` are values to mask
    wherever they appear (e.g. the database password); sensitive-looking environment variables
    are registered too."""
    register_environment_secrets()
    for secret in secrets:
        register_secret(secret)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    handler.addFilter(TelemetryErrorRateLimit())
    handler.set_name(_HANDLER_NAME)

    root = logging.getLogger()
    # Replace only our own handler, so calling this twice doesn't duplicate output and handlers
    # added by other tools (e.g. pytest's log capture) are left alone.
    root.handlers = [h for h in root.handlers if h.get_name() != _HANDLER_NAME]
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn installs its own plain-text handlers; route its logs through ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # Uvicorn's access log would duplicate RequestContextMiddleware's per-request line.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
