"""Per-request context for the API: a request ID, a server span, and one log line per request.

Plain ASGI middleware (not Starlette's BaseHTTPMiddleware), so it works with streaming responses
(SSE). Written here instead of using opentelemetry-instrumentation-fastapi
(docs/decisions/PHASE10_NOTES.md): that package records the full URL including the query
string (upload file names travel in `?filename=`) and adds a span for every ASGI message, which
for an event stream is a span per event. This span records only the method, the route
*template* (`/v1/jobs/{job_id}`), the status code and, for job routes, the job id.

Incoming `traceparent` headers are ignored: every request starts its own trace. Health checks
and /metrics (polled every few seconds by Docker and Prometheus) are not traced.
"""

import logging
import re
import time
import uuid
from contextlib import nullcontext

from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.logging import request_id_var
from app.observability.tracing import set_error, span

REQUEST_ID_HEADER = "X-Request-ID"
UNTRACED_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.decode("latin-1").lower() == REQUEST_ID_HEADER.lower():
            candidate = value.decode("latin-1")
            # fullmatch, not match: with match, "$" would also accept a trailing newline.
            return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None
    return None


def _finish_server_span(server_span: Span, scope: Scope, method: str, status_code: int) -> None:
    route = getattr(scope.get("route"), "path", None)  # the matched template, set by the router
    server_span.update_name(f"{method} {route}" if route else f"{method} (unmatched route)")
    if route:
        server_span.set_attribute("http.route", route)
    server_span.set_attribute("http.response.status_code", status_code)
    job_id = (scope.get("path_params") or {}).get("job_id")
    try:
        server_span.set_attribute("job_id", str(uuid.UUID(str(job_id))))
    except ValueError:
        pass  # absent or not a UUID: client input, not recorded
    if status_code >= 500:
        set_error(server_span)


class RequestContextMiddleware:
    """Assign a request ID (echoed in the response), open a server span, log one line."""

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
        method = scope["method"]

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        traced = scope["path"] not in UNTRACED_PATHS
        span_scope = (
            span(
                method,
                kind=SpanKind.SERVER,
                context=Context(),  # a new trace per request
                attributes={"http.request.method": method},
            )
            if traced
            else nullcontext()
        )
        try:
            with span_scope as server_span:
                try:
                    await self.app(scope, receive, send_with_request_id)
                finally:
                    if server_span is not None:
                        _finish_server_span(server_span, scope, method, status_code)
                    self.logger.info(
                        "request",
                        extra={
                            "method": method,
                            "path": scope["path"],  # path only; query strings may hold file names
                            "status": status_code,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        },
                    )
        finally:
            request_id_var.reset(token)
