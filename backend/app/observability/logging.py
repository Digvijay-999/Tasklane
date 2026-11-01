"""Structured (JSON) logging with a per-request ID.

Each log line is one JSON object, which is easy to search and filter. Phase 10 adds OpenTelemetry
on top of this; it does not replace it.
"""

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_HANDLER_NAME = "tasklane-json"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

# A ContextVar is like Node's AsyncLocalStorage: each request sees its own value, even when many
# requests are handled concurrently.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord has; anything else was passed via `extra=` and is included in output.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "request_id"}
# Uvicorn attaches a terminal-colored copy of some messages; it is noise in JSON.
_IGNORED_ATTRS = {"color_message"}


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in _IGNORED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Send all logs (ours and uvicorn's) to stdout as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
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


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.decode("latin-1").lower() == REQUEST_ID_HEADER.lower():
            candidate = value.decode("latin-1")
            # fullmatch, not match: with match, "$" would also accept a trailing newline.
            return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None
    return None


class RequestContextMiddleware:
    """Assign a request ID, echo it in the response, and log one line per request.

    Written as plain ASGI middleware (rather than Starlette's BaseHTTPMiddleware) because that
    works correctly with streaming responses, which the SSE endpoint will need in Phase 8.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = logging.getLogger("tasklane.request")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500  # if the app crashes before responding, log it as a 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            self.logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],  # path only; query strings may hold sensitive values
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)
