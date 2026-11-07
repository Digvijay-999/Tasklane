"""One error response shape for every route:

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}

- `code` is stable and machine-readable; `message` is safe to show to a user; `details` holds
  safe structured extras; `request_id` matches the X-Request-ID header and the server logs.
- Services raise ServiceError; STATUS_BY_CODE maps each code to an HTTP status.
- Unexpected exceptions become 500 internal_error (503 for database connectivity problems). The
  traceback is logged, never returned.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.logging import request_id_var
from app.services.errors import ErrorCode, ServiceError

logger = logging.getLogger(__name__)

STATUS_BY_CODE: dict[str, int] = {
    ErrorCode.IDEMPOTENCY_KEY_MISSING: 400,
    ErrorCode.IDEMPOTENCY_KEY_INVALID: 400,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.EMPTY_FILE: 400,
    ErrorCode.UPLOAD_INTERRUPTED: 400,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.ARTIFACT_NOT_FOUND: 404,
    ErrorCode.ARTIFACT_GONE: 410,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.UPLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_FILE_TYPE: 415,
    ErrorCode.CONTENT_TYPE_MISMATCH: 415,
    ErrorCode.FILE_SIGNATURE_MISMATCH: 415,
    ErrorCode.INVALID_FILENAME: 422,
    ErrorCode.UPLOAD_NOT_FOUND: 422,
    ErrorCode.UPLOAD_NOT_READY: 422,
    ErrorCode.UPLOAD_EXPIRED: 422,
    ErrorCode.UPLOAD_KIND_MISMATCH: 422,
}

# Codes produced here rather than by services.
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
REQUEST_TOO_LARGE = "request_too_large"
MALFORMED_BODY = "malformed_body"
INVALID_REQUEST = "invalid_request"
INTERNAL_ERROR = "internal_error"
SERVICE_UNAVAILABLE = "service_unavailable"

_HTTP_ERRORS = {
    # FastAPI raises a bare 400 when it can't read or parse a body for reasons other than invalid
    # JSON syntax (e.g. bytes that aren't UTF-8, absurdly deep nesting).
    400: (MALFORMED_BODY, "The request body could not be read or parsed."),
    404: (NOT_FOUND, "No route matches this URL."),
    405: (METHOD_NOT_ALLOWED, "This HTTP method is not allowed for this URL."),
    413: (REQUEST_TOO_LARGE, "The request body is too large."),
}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id_var.get(),
        }
    }


def error_response(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, details))


async def _service_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)
    return error_response(STATUS_BY_CODE.get(exc.code, 400), exc.code, exc.message, exc.details)


# Pydantic messages for these error types quote part of the submitted value (e.g. "found `S` at
# 1"), so they are replaced with fixed text.
_FIXED_ISSUES = {
    "uuid_parsing": "Input should be a valid UUID.",
    "uuid_type": "Input should be a valid UUID.",
    "json_invalid": "Invalid JSON.",
    "datetime_parsing": "Input should be a valid datetime.",
    "datetime_from_date_parsing": "Input should be a valid datetime.",
}


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = []
    for error in exc.errors():
        location, *path = error.get("loc", ("body",))
        # Only the location, field path, and an explanation: never the submitted value, which
        # Pydantic includes in the raw error by default and could contain anything.
        issue = _FIXED_ISSUES.get(error.get("type", ""), error.get("msg", "Invalid value."))
        fields.append(
            {
                "location": str(location),
                "field": ".".join(str(part) for part in path),
                "issue": issue,
            }
        )
    return error_response(
        422, INVALID_REQUEST, "The request is invalid. See details.fields.", {"fields": fields}
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code, message = _HTTP_ERRORS.get(exc.status_code, ("http_error", "The request failed."))
    return error_response(exc.status_code, code, message)


class ErrorHandlingMiddleware:
    """Turn any exception that escaped the route into a safe 500/503 response.

    Starlette's own catch-all runs *outside* the request-ID middleware, so its responses would
    have no request id. This sits inside it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            logger.exception("unhandled error", extra={"path": scope.get("path")})
            if response_started:
                raise  # too late to send an error response; let the server close the connection
            # Database unreachable, or every pooled connection busy for pool_timeout seconds.
            if isinstance(exc, (OperationalError, InterfaceError, PoolTimeoutError)):
                response = error_response(
                    503,
                    SERVICE_UNAVAILABLE,
                    "A required service is temporarily unavailable. Try again shortly.",
                )
            else:
                response = error_response(500, INTERNAL_ERROR, "An unexpected error occurred.")
            await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """Limit request bodies on every route except the exempt ones (uploads have their own limits).

    Counts the bytes actually received; Content-Length is only used to reject early.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, exempt_paths: frozenset[str]) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _route_path(scope) in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            response = error_response(
                413,
                REQUEST_TOO_LARGE,
                "The request body is too large.",
                {"limit_bytes": self.max_bytes},
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises HTTPException from body reading unchanged, and the
                    # handler below turns it into the standard 413 error.
                    raise StarletteHTTPException(status_code=413)
            return message

        await self.app(scope, limited_receive, send)


def _route_path(scope: Scope) -> str:
    """The path the router matches on: without the root path prefix, if the app is mounted
    under one (e.g. uvicorn --root-path /api)."""
    path, root = scope["path"], scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root) else path


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def install_error_handling(
    app: FastAPI, *, max_json_body_bytes: int, exempt_paths: set[str]
) -> None:
    """Register handlers and middleware. Call before adding the request-ID middleware, so that
    one ends up outermost (Starlette wraps the most recently added middleware around the rest)."""
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=max_json_body_bytes,
        exempt_paths=frozenset(exempt_paths),
    )
    app.add_middleware(ErrorHandlingMiddleware)
