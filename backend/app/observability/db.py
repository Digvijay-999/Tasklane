"""Database spans: one CLIENT span per SQL statement, but only inside a traced operation.

Hand-written SQLAlchemy event hooks instead of the opentelemetry-instrumentation-sqlalchemy
package (reasons in docs/decisions/PHASE10_NOTES.md): spans are created only when a recording
span is current (a request, a job attempt, a recovery action), so the dispatcher's idle polls,
heartbeats and SSE polls add nothing; and the attributes are exactly these:

- db.system = "postgresql", db.operation = the statement's first keyword (SELECT, UPDATE, ...);
- db.statement = the SQL text with its placeholders (%(job_id)s), never the parameter values.
  SQLAlchemy always sends values separately as bound parameters, so the text never contains
  them (a test checks this with a distinctive value).
"""

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import Engine, event

from app.observability.tracing import SUPPRESS_DB_SPANS, tracer

MAX_STATEMENT_CHARS = 1000
_SPAN_KEY = "_tasklane_otel_span"


def instrument_engine(engine: Engine) -> None:
    """Add the span hooks to one engine (every process calls this through create_db_engine)."""
    event.listen(engine, "before_cursor_execute", _before_execute)
    event.listen(engine, "after_cursor_execute", _after_execute)
    event.listen(engine, "handle_error", _on_error)


def _before_execute(
    conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    if context is None or otel_context.get_value(SUPPRESS_DB_SPANS):
        return
    if not trace.get_current_span().is_recording():
        return  # not inside a traced operation: no orphan spans for background polls
    words = statement.split(None, 1)
    operation = words[0].upper() if words else "SQL"
    setattr(
        context,
        _SPAN_KEY,
        tracer().start_span(
            f"db {operation}",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "postgresql",
                "db.operation": operation,
                "db.statement": statement[:MAX_STATEMENT_CHARS],
            },
        ),
    )


def _after_execute(
    conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    db_span = getattr(context, _SPAN_KEY, None)
    if db_span is not None:
        db_span.end()
        setattr(context, _SPAN_KEY, None)


def _on_error(exception_context: Any) -> None:
    context = exception_context.execution_context
    db_span = getattr(context, _SPAN_KEY, None) if context is not None else None
    if db_span is None:
        return
    original = exception_context.original_exception
    # The exception type and SQLSTATE only: driver messages can echo values and host names.
    db_span.set_attribute("error.type", type(original).__name__)
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate:
        db_span.set_attribute("db.response.status_code", str(sqlstate))
    db_span.set_status(Status(StatusCode.ERROR))
    db_span.end()
    setattr(context, _SPAN_KEY, None)
